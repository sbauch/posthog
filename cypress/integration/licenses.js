describe('Licenses', () => {
    it('Licenses loaded', () => {
        cy.get('[data-attr=menu-item-instance]').click()
        cy.get('[data-attr=menu-item-instance-licenses]').click()
        cy.get('h1').should('contain', 'Licenses')
    })
})
